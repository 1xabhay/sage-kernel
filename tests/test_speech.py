"""Reading the model's `format` output: the reply as it streams, then the whole answer; and what may be said."""

import unittest

from sage import speech

LABELS = ("cheerful1", "sad1")
SENTENCE = 'Yay cheerful1! I *bounces* saw 🎉 your cat at 6, "Miso".'


class PartialReply(unittest.TestCase):
    def test_the_reply_decodes_as_it_streams_and_closes_on_its_quote(self):
        cases = {"": ("", False), '{"re': ("", False), '{"reply": "Hel': ("Hel", False),
                 '{ "reply" : "ok", "emote"': ("ok", True), '{"reply": "a\\': ("a", False),
                 '{"reply": "Hi \\"you\\"\\n"': ('Hi "you"\n', True), '{"reply": "caf\\u00e9"}': ("café", True),
                 '{"reply": "x\\u00': ("x", False), '{"emote": {"name": "sad1"}, "reply": "Hi"}': ("", False)}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(speech.partial_reply(raw), expected)


class Output(unittest.TestCase):
    def test_actions_come_only_from_the_structured_answer(self):
        self.assertEqual(speech.parse_output('{"reply": "Yay!", "emote": {"name": "cheerful1"}, "wave": null}'),
                         ("Yay!", (("emote", {"name": "cheerful1"}),), ""))

    def test_a_broken_answer_keeps_the_reply_it_decoded_and_takes_no_action(self):
        reply, calls, problem = speech.parse_output('{"reply": "Hello there", "emote": {"na')
        self.assertEqual((reply, calls), ("Hello there", ()))
        self.assertTrue(problem)
        self.assertTrue(speech.parse_output('["Hi"]')[2])
        self.assertTrue(speech.parse_output('{"reply": 3}')[2])


class Speakable(unittest.TestCase):
    def test_labels_emoji_and_stage_directions_are_never_said_but_digits_are(self):
        self.assertEqual(speech.speakable(SENTENCE, LABELS, closed=True), 'Yay ! I saw your cat at 6, "Miso".')

    def test_a_tool_named_in_brackets_or_as_a_label_is_never_said_but_its_word_is(self):
        tools = ("emote", "wave")
        for said, heard in (("Sure (emote: ) thing.", "Sure thing."), ("[emote] Hi!", "Hi!"),
                            ("Hi (wave: hello) there.", "Hi there."), ("emote: Hello.", "Hello."),
                            ("I like to wave at you.", "I like to wave at you."),
                            ("It is (really) fun.", "It is (really) fun.")):
            with self.subTest(said=said):
                self.assertEqual(speech.speakable(said, (), True, tools), heard)
        self.assertEqual(speech.speakable("Sure (emo", (), False, tools), "Sure ")
        live = 'Okay. (emote: ) Hello [wave] there, (really) fine.'
        final = speech.speakable(live, (), True, tools)
        for end in range(len(live) + 1):
            with self.subTest(prefix=live[:end]):
                self.assertTrue(final.startswith(speech.speakable(live[:end], (), False, tools)))

    def test_a_reply_cut_by_the_cap_keeps_only_whole_sentences(self):
        story = "The sea is vast. Once upon a time there was a ship. "
        cut = (story + "x" * 200)[:160]
        self.assertEqual(speech.whole_sentences(cut, 160), "The sea is vast. Once upon a time there was a ship.")
        self.assertEqual(speech.whole_sentences("word " * 40, 160).strip(), ("word " * 32).strip())  # no sentence end
        self.assertEqual(speech.whole_sentences("Short and done", 160), "Short and done")  # not at the cap

    def test_while_streaming_a_sentence_after_the_first_is_held_until_it_ends(self):
        """Live eval: "…for you. If there's anything … or if you need" streamed, then the cap cut the reply
        back to "…for you.", so the robot spoke words the journal never held."""
        first = "Cute voices can be fun! I'll try my best. "
        self.assertEqual(speech.near_cap("Cute voices can ", 160), "Cute voices can ")  # the first streams
        self.assertEqual(speech.near_cap(first + "If you ", 160), first.rstrip())
        self.assertEqual(speech.near_cap(first + "If you need. ", 160), first + "If you need. ")
        live = ("Cute voices can be fun! I'll try my best to keep things light and engaging for you. If there's "
                "anything you'd like to try with a cute voice or if you need help")[:160]
        final = speech.whole_sentences(live, 160)
        for end in range(len(live) + 1):
            with self.subTest(prefix=live[:end]):
                self.assertTrue(final.startswith(speech.near_cap(live[:end], 160).rstrip()))

    def test_stray_non_latin_letters_are_dropped(self):
        self.assertEqual(speech.speakable("They met\u86df.", (), True), "They met.")
        self.assertEqual(speech.speakable("A caf\u00e9 na\u00efve.", (), True), "A caf\u00e9 na\u00efve.")

    def test_kernel_annotations_never_reach_speech(self):
        for said, heard in (("I'll (interrupted)", "I'll"), ("(interrupted) Okay.", "Okay."),
                            ('Sure (the person interrupted you after: "Hi") fine.', "Sure fine."),
                            ("Hi (your reachy body could not do emote: timeout).", "Hi ."),
                            ("[JSON fields you may add: emote] Hello.", "Hello."),
                            ("It was (really) interrupting.", "It was (really) interrupting.")):
            with self.subTest(said=said):
                self.assertEqual(speech.speakable(said, (), True), heard)

    def test_talk_of_being_interrupted_is_never_spoken(self):
        """Live run 5b said "You're right. I was interrupted.", paraphrasing the kernel's barge-in note."""
        for said, heard in (("You're right. I was interrupted.", "You're right."),
                            ("Sorry, I was interrupted. Go on.", "Go on."), ("You interrupted me!", ""),
                            ("I got cut off. Anyway, hi.", "Anyway, hi."), ("You keep cutting me off.", ""),
                            ("It was (really) interrupting.", "It was (really) interrupting."),
                            ("I was thinking of you.", "I was thinking of you.")):
            with self.subTest(said=said):
                self.assertEqual(speech.speakable(said, (), True), heard)
        live = "You're right. Sorry, I was interrupted. I was thinking of you."
        final = speech.speakable(live, (), True)
        for end in range(len(live) + 1):
            with self.subTest(prefix=live[:end]):
                self.assertTrue(final.startswith(speech.speakable(live[:end], (), False)))

    def test_streaming_holds_back_an_unfinished_word_or_stage_direction(self):
        self.assertEqual(speech.speakable("Hello the", LABELS, closed=False), "Hello ")
        self.assertEqual(speech.speakable("Hi *bounces hap", LABELS, closed=False), "Hi ")
        self.assertEqual(speech.speakable("Hi cheerful", LABELS, closed=False), "Hi ")

    def test_what_streams_is_always_a_prefix_of_what_is_finally_said(self):
        final = speech.speakable(SENTENCE, LABELS, closed=True)
        for end in range(len(SENTENCE) + 1):
            with self.subTest(prefix=SENTENCE[:end]):
                self.assertTrue(final.startswith(speech.speakable(SENTENCE[:end], LABELS, closed=False)))


if __name__ == "__main__":
    unittest.main()
