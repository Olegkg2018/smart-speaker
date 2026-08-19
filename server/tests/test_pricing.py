from app.pricing import CostMeter, rates_for


def test_mini_is_not_priced_as_full_model():
    mini = rates_for("gpt-realtime-2.1-mini")
    full = rates_for("gpt-realtime-2.1")
    assert mini is not None and full is not None
    # Ловушка длинных префиксов: mini не должна совпасть с "gpt-realtime".
    assert mini.audio_out < full.audio_out


def test_dated_model_name_matches_prefix():
    assert rates_for("gpt-realtime-2025-08-28") is not None


def test_unknown_model_has_no_rates():
    assert rates_for("some-other-model") is None


class _Usage:
    def __init__(self, audio_in=0, text_in=0, cached_audio=0, audio_out=0, text_out=0):
        self.input_token_details = type(
            "In",
            (),
            {
                "audio_tokens": audio_in,
                "text_tokens": text_in,
                "cached_tokens_details": type(
                    "Cached", (), {"audio_tokens": cached_audio, "text_tokens": 0}
                )(),
            },
        )()
        self.output_token_details = type(
            "Out", (), {"audio_tokens": audio_out, "text_tokens": text_out}
        )()


def test_cost_accumulates_across_turns():
    meter = CostMeter("gpt-realtime-2.1")
    meter.add(_Usage(audio_in=1000, audio_out=1000))
    first = meter.total_usd
    assert first > 0
    meter.add(_Usage(audio_in=1000, audio_out=1000))
    assert meter.total_usd == first * 2
    assert meter.turns == 2


def test_cached_tokens_are_not_double_charged():
    plain = CostMeter("gpt-realtime-2.1")
    plain.add(_Usage(audio_in=1000))

    cached = CostMeter("gpt-realtime-2.1")
    # Те же 1000 токенов, но все из кэша — должно выйти заметно дешевле.
    cached.add(_Usage(audio_in=1000, cached_audio=1000))

    assert cached.total_usd < plain.total_usd


def test_unknown_model_costs_nothing_and_does_not_crash():
    meter = CostMeter("mystery-model")
    assert meter.add(_Usage(audio_in=1000, audio_out=1000)) == 0.0
    assert meter.total_usd == 0.0


def test_missing_usage_is_ignored():
    meter = CostMeter("gpt-realtime-2.1")
    assert meter.add(None) == 0.0
