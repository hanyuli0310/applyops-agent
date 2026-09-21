from applyops.answers import AnswerStore


def test_similar_sponsorship_questions_share_one_confirmed_answer(tmp_path):
    store = AnswerStore(tmp_path)
    store.set_answer("Will you require visa sponsorship now or in the future?", "Yes")

    match = store.resolve("Do you now or later need sponsorship to work in the US?")

    assert match is not None
    assert match.answer == "Yes"
    assert match.answer_class == "sponsorship"


def test_answer_classes_do_not_cross_contaminate_compensation_questions(tmp_path):
    store = AnswerStore(tmp_path)
    store.set_answer("Are you comfortable working on W2 basis?", "Yes")

    assert store.resolve("What is your expected salary?") is None


def test_unclassified_questions_still_require_exact_text(tmp_path):
    store = AnswerStore(tmp_path)
    store.set_answer("Why do you want to work here?", "The mission fits my experience.")

    assert store.resolve("Why are you interested in this company?") is None
