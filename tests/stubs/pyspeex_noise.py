"""Заглушка pyspeex_noise: на этой машине нет колеса для Python 3.14.

Конвейер импортирует AudioProcessor при загрузке. Текстовый запуск его не зовёт.
"""


class _Processed:
    """Результат обработки куска: те же байты без шумоподавления."""

    def __init__(self, audio: bytes) -> None:
        self.audio = audio


class AudioProcessor:
    """Обработчик звука без нативной библиотеки."""

    def __init__(self, auto_gain: int, noise_suppression: int) -> None:
        del auto_gain, noise_suppression

    def Process10ms(self, audio: bytes) -> _Processed:
        """Вернуть кусок без изменений."""
        return _Processed(audio)
