import pytest

from core.windows_clipboard import ClipboardChangedError, paste_text_transaction


class FakeClipboard:
    def __init__(self):
        self.content = {13: "사용자 원문", 8: b"image-bits", 49000: b"custom-format"}
        self.counter = 10
        self.opened = False
        self.disposed = False
        self.fail_snapshot = False
        self.fail_prepare = False
        self.fail_replace_once = False
        self.replacements = 0
        self.own_content = False
        self.synthesize_on_close = False

    def open(self):
        assert not self.opened
        self.opened = True

    def close(self):
        if self.opened and self.synthesize_on_close:
            self.counter += 1
        self.opened = False

    def dispose(self):
        self.disposed = True

    def sequence(self):
        return self.counter

    def owns_temporary_text(self, text):
        return self.own_content and self.content.get(13) == text

    def snapshot(self):
        assert self.opened
        if self.fail_snapshot:
            raise RuntimeError("cannot copy every original format")
        return [[key, value] for key, value in self.content.items()]

    def prepare_text(self, text):
        if self.fail_prepare:
            raise MemoryError("prepare failed")
        return [[13, text]]

    def replace(self, handles):
        assert self.opened
        self.replacements += 1
        self.content = {}
        self.counter += 1
        self.own_content = True
        if self.fail_replace_once:
            self.fail_replace_once = False
            raise RuntimeError("set failed after empty")
        for entry in handles:
            self.content[entry[0]] = entry[1]
            entry[1] = None

    def release(self, handles):
        handles.clear()


@pytest.mark.parametrize("text", ["한글", "A 😀 B", "첫째\n둘째", "끝 공백  ", "줄 끝\n"])
def test_paste_restores_text_image_and_custom_formats(text):
    api = FakeClipboard()
    before = dict(api.content)

    def paste():
        assert not api.opened
        assert api.content == {13: text}
        return "consumed"

    assert paste_text_transaction(text, paste, api=api) == "consumed"
    assert api.content == before
    assert api.disposed and not api.opened


def test_paste_failure_restores_every_original_format():
    api = FakeClipboard()
    before = dict(api.content)

    def paste():
        raise RuntimeError("target lost focus")

    with pytest.raises(RuntimeError, match="target lost focus"):
        paste_text_transaction("본문", paste, api=api)
    assert api.content == before
    assert api.disposed


def test_new_user_clipboard_during_paste_is_never_overwritten():
    api = FakeClipboard()

    def paste():
        api.content = {13: "사용자가 새로 복사함"}
        api.own_content = False
        api.counter += 1

    with pytest.raises(ClipboardChangedError):
        paste_text_transaction("본문", paste, api=api)
    assert api.content == {13: "사용자가 새로 복사함"}
    assert api.replacements == 1
    assert api.disposed


def test_close_time_synthesized_formats_do_not_look_like_a_new_user_copy():
    api = FakeClipboard()
    before = dict(api.content)
    api.synthesize_on_close = True
    paste_text_transaction("본문", lambda: None, api=api)
    assert api.content == before


def test_paste_time_synthesized_formats_restore_the_original_snapshot():
    api = FakeClipboard()
    before = dict(api.content)

    def paste():
        api.content[1] = b"synthesized ansi text"
        api.counter += 1

    paste_text_transaction("본문", paste, api=api)
    assert api.content == before


def test_identical_text_newly_copied_by_user_is_still_preserved():
    api = FakeClipboard()

    def paste():
        api.content = {13: "본문"}
        api.own_content = False
        api.counter += 1

    with pytest.raises(ClipboardChangedError):
        paste_text_transaction("본문", paste, api=api)
    assert api.content == {13: "본문"}
    assert api.replacements == 1


def test_user_copy_between_publication_close_and_callback_is_not_consumed_or_overwritten():
    class CloseRaceClipboard(FakeClipboard):
        def close(self):
            super().close()
            if self.replacements == 1 and self.own_content:
                self.content = {13: "새로 복사한 사용자 내용"}
                self.own_content = False
                self.counter += 1

    api = CloseRaceClipboard()
    with pytest.raises(ClipboardChangedError, match="붙여넣기 전에"):
        paste_text_transaction("본문", lambda: pytest.fail("must not read the newer clipboard"), api=api)
    assert api.content == {13: "새로 복사한 사용자 내용"}
    assert api.replacements == 1


def test_unchanged_sequence_does_not_override_new_clipboard_ownership():
    api = FakeClipboard()

    def paste():
        api.content = {13: "본문"}
        api.own_content = False  # Sequence can wrap or be sampled late.

    with pytest.raises(ClipboardChangedError):
        paste_text_transaction("본문", paste, api=api)
    assert api.content == {13: "본문"}
    assert api.replacements == 1


@pytest.mark.parametrize("failure", ["fail_snapshot", "fail_prepare"])
def test_capture_failure_never_empties_the_clipboard(failure):
    api = FakeClipboard()
    before = dict(api.content)
    setattr(api, failure, True)
    with pytest.raises((RuntimeError, MemoryError)):
        paste_text_transaction("본문", lambda: pytest.fail("must not paste"), api=api)
    assert api.content == before
    assert api.replacements == 0
    assert api.disposed


def test_partial_install_failure_rolls_back_before_callback():
    api = FakeClipboard()
    before = dict(api.content)
    api.fail_replace_once = True
    with pytest.raises(RuntimeError, match="set failed"):
        paste_text_transaction("본문", lambda: pytest.fail("must not paste"), api=api)
    assert api.content == before
    assert api.disposed


@pytest.mark.parametrize("invalid", ["", "a\0b", None, 3])
def test_invalid_body_never_touches_clipboard(invalid):
    api = FakeClipboard()
    with pytest.raises(ValueError):
        paste_text_transaction(invalid, lambda: None, api=api)
    assert not api.disposed  # Validation happens before the API is acquired.
    assert api.counter == 10
