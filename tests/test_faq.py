"""The About tab's FAQ: structure, uniqueness, and collapsed-by-default."""


def test_every_entry_is_a_question_and_answer(shared_app):
    rows = shared_app._faq_rows
    assert len(rows) >= 30
    for row in rows:
        assert row["question"].startswith("Q:")
        assert row["answer"].startswith("A:")
        assert len(row["answer"]) > 20, row["question"]


def test_questions_are_unique(shared_app):
    questions = [r["question"] for r in shared_app._faq_rows]
    assert len(questions) == len(set(questions))


def test_all_answers_start_collapsed(shared_app):
    for row in shared_app._faq_rows:
        assert row["open"] is False
        assert not row["frame"].winfo_manager(), row["question"]


def test_toggle_expands_in_place_and_collapses_again(app):
    row = app._faq_rows[3]
    app._faq_toggle(row)
    assert row["open"] and row["frame"].winfo_manager() == "pack"
    assert row["frame"].master is row["chev"].master.master  # own container
    assert row["chev"].cget("text") == "▾"
    app._faq_toggle(row)
    assert not row["open"] and not row["frame"].winfo_manager()
    assert row["chev"].cget("text") == "▸"


def test_expand_all_and_collapse_all(app):
    app._faq_set_all(True)
    assert all(r["open"] for r in app._faq_rows)
    app._faq_set_all(False)
    assert not any(r["open"] for r in app._faq_rows)


def test_retired_and_stale_content_is_gone(shared_app):
    text = " ".join(r["question"] + r["answer"] for r in shared_app._faq_rows)
    assert "known issue in v1.0" not in text          # archaeology retired
    assert "Scan All" not in text                     # button is "Scan for new"
    assert "base save directory" not in text          # activity.log moved
    # The old "can't add URLs mid-download" answer contradicted reality.
    assert "URL field and Add to Batch button are disabled" not in text
