"""Names the voice must hear and say right (SAGE_VOICE_NAMES): here the owner's is Mika.

Live 2026-10-23: the STT heard "Mika" as "Micah" and the kernel remembered
"Their name is Micah."; Kokoro, an English voice, reads "Mika" near "Mica".
The owner picked "Mee-kah" by ear.
"""

from sage_device import names

SPEC = "Mika:Mee-kah:Micah|Mica|Meeka"


def test_the_setting_names_a_word_how_to_say_it_and_what_the_stt_hears_instead():
    (name,) = names.parse(SPEC)
    assert (name.written, name.spoken) == ("Mika", "Mee-kah")
    assert name.heard == ("Micah", "Mica", "Meeka")
    assert names.parse("") == ()
    assert names.parse("Sage::")[0].spoken == "Sage"  # no respelling: said as written


def test_what_the_stt_hears_instead_is_corrected_as_a_whole_word():
    known = names.parse(SPEC)
    assert names.correct("My name is Micah.", known) == "My name is Mika."
    assert names.correct("MICA, hi", known) == "Mika, hi"
    # Inside a word: left alone.
    assert names.correct("Micahville is a city", known) == "Micahville is a city"


def test_the_voice_is_sent_the_respelling_and_captions_come_back_written():
    known = names.parse(SPEC)
    assert names.spoken("Hi Mika, nice to see you.", known) == "Hi Mee-kah, nice to see you."
    assert names.written("Mee-kah,", known) == "Mika,"
    assert names.written("nice", known) == "nice"


def test_a_reply_with_a_misheard_name_says_the_right_one():
    """Live 2026-10-27: the kernel remembered "Micah", and the voice said it to Mika."""
    known = names.parse(SPEC)
    assert names.spoken("Micah, I hear you.", known) == "Mee-kah, I hear you."
    assert names.spoken("Micahville is a city", known) == "Micahville is a city"
    assert names.spoken("Tomoh, hi", names.parse(TABLE)) == "Tomo, hi"  # said as written


def test_a_name_being_disowned_stays_as_remembered_so_it_can_be_forgotten():
    """Corrected, "Forget Micah, my name is Mika" would have the kernel forget the right name."""
    known = names.parse(SPEC)
    # The STT hears "Mika" as "Micah" too: only the disowned one is kept.
    assert names.correct("Forget Micah, my name is Micah.", known) == "Forget Micah, my name is Mika."
    assert names.correct("My name is Micah, not Mica.", known) == "My name is Mika, not Micah."
    assert names.correct("forget, MICA.", known) == "forget, Micah."  # as first listed: as the kernel holds it
    assert names.correct("Forget Mika.", known) == "Forget Mika."


#: Made-up names, as SAGE_VOICE_NAMES gives them: each with the STT's variants of it.
TABLE = "Kiri:Kee-ree:Keery|Kiree|Kearie,Tomo::Tomoh|Tomow"
FRAMES = ["My name is {}.", "{}, hi", "Hi {}!", "It's {}'s turn.", "{}"]


def test_p2_every_listed_variant_becomes_the_name():
    """P2: every variant in the table, and the respelling heard back, comes out as the name."""
    known = names.parse(TABLE)
    for name in known:
        for variant in {*name.heard, name.spoken, name.written}:
            for frame in FRAMES:
                for said in (variant, variant.lower(), variant.upper()):
                    expected = frame.format(name.written)
                    assert names.correct(frame.format(said), known) == expected, said
