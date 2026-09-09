from abc import ABC, abstractmethod


class Separator(ABC):
    @abstractmethod
    def separate(self, mix, vocals, instrumental, *, profile=None, cancelled=None): ...


class PitchEstimator(ABC):
    @abstractmethod
    def estimate(self, audio): ...


class Transcriber(ABC):
    @abstractmethod
    def transcribe(self, audio, language, *, context: str = ""): ...


class Aligner(ABC):
    @abstractmethod
    def align(self, audio, text, language): ...
